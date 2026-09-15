"""Сканер рынка: ищет точки входа и заводит сигналы в журнал."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from app.config import settings
from app.market.feed import feed, split_symbol
from app.news.calendar import calendar
from app.storage import repo
from app.storage.settings_store import config
from app.strategy.trend_momentum import strategy

log = logging.getLogger("engine.scanner")

SignalCallback = Callable[[repo.Signal], Awaitable[None]]


class Scanner:
    """Периодически опрашивает рынок и создаёт сигналы.

    Намеренные ограничения, каждое из которых лечит конкретную болезнь:

    * анализ только по ЗАКРЫТЫМ свечам — иначе сигнал «мигает» внутри свечи
      и не совпадает с тем, что трейдер видит на графике;
    * один активный сигнал на инструмент — чтобы не было каши из
      противоречивых рекомендаций;
    * кулдаун после сигнала — защита от серии входов на одном движении;
    * одна свеча — максимум один сигнал.
    """

    def __init__(self, on_signal: SignalCallback | None = None) -> None:
        self.on_signal = on_signal
        self.running = False
        self.last_scan_at: int | None = None
        self.scans_done = 0
        self.last_error: str | None = None
        # Одна свеча — один сигнал, но у каждого получателя свой счёт:
        # ключ (получатель, инструмент)
        self._last_candle_ts: dict[tuple[int, str], int] = {}
        self._by_owner: dict[int, list[str]] = {}
        self._task: asyncio.Task | None = None

    # ----------------------------------------------------------------
    # Запуск
    # ----------------------------------------------------------------

    async def prepare(self) -> dict[int, list[str]]:
        """Проверяет, какие инструменты доступны — отдельно для каждого.

        Возвращает {получатель: [инструменты]}. Список у каждого свой,
        поэтому и проверять приходится по отдельности.
        """
        by_owner: dict[int, list[str]] = {}
        wanted_all: set[str] = set()

        for owner_id in settings.owner_id_list:
            wanted = list(config.get("symbols", user_id=owner_id) or [])
            wanted_all.update(wanted)
            by_owner[owner_id] = wanted

        if not wanted_all:
            self._by_owner = {}
            return {}

        try:
            ok, missing = await feed.validate_symbols(sorted(wanted_all))
        except Exception as exc:
            self.last_error = str(exc)
            log.error("Не удалось загрузить список инструментов: %s", exc)
            return {}

        available = set(ok)
        if missing:
            self._report_missing(missing)

        result = {
            owner_id: [s for s in symbols if s in available]
            for owner_id, symbols in by_owner.items()
        }
        for owner_id, symbols in result.items():
            if symbols:
                log.info("Получатель %s: в работе %s", owner_id, ", ".join(symbols))
        self._by_owner = result
        return result

    def _report_missing(self, missing: list[str]) -> None:
        """Сообщение должно называть ту площадку, к которой относится пара."""
        by_place: dict[str, list[str]] = {}
        for full in missing:
            prefix, _ = split_symbol(full)
            place = (
                feed.pocket_broker.title
                if prefix == "po"
                else settings.exchange.upper()
            )
            by_place.setdefault(place, []).append(full)

        for place, items in by_place.items():
            reason = ""
            if place == feed.pocket_broker.title and not feed.pocket_broker.configured:
                reason = " (не задан ключ доступа)"
            log.warning(
                "Недоступны на %s%s и будут пропущены: %s",
                place, reason, ", ".join(items),
            )

    def start(self) -> asyncio.Task:
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="scanner")
        return self._task

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await self.prepare()
        await calendar.refresh(force=True)

        while self.running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("Ошибка цикла сканирования: %s", exc)
                await repo.log_event("error", f"Сбой сканера: {exc}")
            await asyncio.sleep(config.get("scan_interval"))

    # ----------------------------------------------------------------
    # Один проход
    # ----------------------------------------------------------------

    async def scan_once(self) -> list[repo.Signal]:
        """Один полный проход по всем инструментам."""
        # Перечитываем настройки — заказчик мог поменять их из приложения
        # минуту назад, и они должны примениться без перезапуска.
        previous_symbols = self._all_symbols()
        previous_ssid = feed.pocket_broker.ssid
        await config.load()
        # SSID брокера мог измениться в приложении — подхватываем без перезапуска
        feed.apply_settings(pocket_ssid=str(config.get("po_ssid") or ""))

        if self._all_symbols() != previous_symbols:
            log.info("Список инструментов изменён, перепроверяю доступность")
            self._by_owner = {}
        elif feed.pocket_broker.ssid != previous_ssid:
            # Без этого пары брокера, отвергнутые при старте из-за пустого
            # ключа, так и оставались бы в списке недоступных навсегда
            log.info("Ключ доступа брокера изменён, перепроверяю его инструменты")
            self._by_owner = {}

        await calendar.refresh()

        if not self._by_owner:
            await self.prepare()
            if not self._by_owner:
                return []

        created: list[repo.Signal] = []

        # Каждый получатель разбирается отдельно: у него свои часы работы,
        # свой дневной лимит и свои настройки фильтра новостей
        for owner_id, symbols in self._by_owner.items():
            if not symbols:
                continue
            cfg = config.view(owner_id)

            if not self._within_trade_hours(cfg):
                continue

            limit = int(cfg.get("max_signals_per_day") or 0)
            if limit > 0:
                since = int(time.time()) - 86400
                today = len(
                    await repo.list_signals(
                        limit=limit + 1, since=since, owner_id=owner_id
                    )
                )
                if today >= limit:
                    log.info(
                        "Получатель %s: дневной лимит исчерпан (%d)", owner_id, limit
                    )
                    continue

            muted_by = calendar.mute_reason(cfg)
            if muted_by is not None:
                minutes = muted_by.minutes_from()
                when = (
                    f"через {minutes:.0f} мин" if minutes > 0
                    else f"{-minutes:.0f} мин назад"
                )
                log.info(
                    "Получатель %s: тишина из-за новости %s (%s)",
                    owner_id, muted_by.title, when,
                )
                continue

            for symbol in symbols:
                try:
                    signal = await self._scan_symbol(symbol, owner_id, cfg)
                    if signal is not None:
                        created.append(signal)
                except Exception as exc:
                    self.last_error = str(exc)
                    log.warning(
                        "Ошибка анализа %s для %s: %s", symbol, owner_id, exc
                    )

        self.last_scan_at = int(time.time())
        self.scans_done += 1
        return created

    async def _scan_symbol(
        self, symbol: str, owner_id: int, cfg
    ) -> repo.Signal | None:
        # Уже есть активный сигнал у ЭТОГО получателя — новый не нужен
        active = await repo.get_active_signals(symbol, owner_id=owner_id)
        if active:
            return None

        # Пауза после предыдущего сигнала — тоже личная
        last_at = await repo.last_signal_at(symbol, owner_id=owner_id)
        if last_at and (time.time() - last_at) < cfg.get("cooldown_min") * 60:
            return None

        raw = await feed.fetch_candles(
            symbol, cfg.get("timeframe"), limit=max(strategy.min_candles(cfg) + 20, 320)
        )
        candles = raw.closed_only()
        if len(candles) < strategy.min_candles(cfg):
            log.debug(
                "%s: свечей %d, нужно %d — жду накопления",
                symbol,
                len(candles),
                strategy.min_candles(cfg),
            )
            return None

        # Одна свеча — один сигнал
        last_candle = candles.last
        if last_candle is None:
            return None
        if self._last_candle_ts.get((owner_id, symbol)) == last_candle.ts:
            return None

        htf_raw = await feed.fetch_candles(symbol, cfg.get("htf_timeframe"), limit=320)
        htf = htf_raw.closed_only()

        result = strategy.analyze(candles, htf, cfg)
        if result is None:
            return None

        self._last_candle_ts[(owner_id, symbol)] = last_candle.ts

        if result.confidence < cfg.get("min_confidence"):
            log.debug(
                "%s: сигнал %s отброшен, уверенность %d < %d",
                symbol,
                result.side,
                result.confidence,
                cfg.get("min_confidence"),
            )
            return None

        broker_id, _ = split_symbol(symbol)
        is_binary = feed.is_binary(symbol)

        expiry_at: int | None = None
        payout: float | None = None
        reasons = list(result.reasons)

        if is_binary:
            expiry_min = int(cfg.get("po_expiry_min") or 5)
            expiry_at = int(time.time()) + expiry_min * 60
            payout = await feed.payout(symbol)

            min_payout = float(cfg.get("po_min_payout") or 0)
            if payout is not None and payout < min_payout:
                log.info(
                    "%s: сигнал отброшен, выплата %.0f%% ниже порога %.0f%%",
                    symbol, payout, min_payout,
                )
                return None

            reasons.append(f"Экспирация через {expiry_min} мин")
            if payout is not None:
                reasons.append(f"Выплата брокера {payout:.0f}%")

        signal = await repo.create_signal(
            symbol=symbol,
            side=result.side,
            timeframe=cfg.get("timeframe"),
            entry=result.entry,
            stop_loss=result.stop_loss,
            take_profit=result.take_profit,
            confidence=result.confidence,
            reasons=reasons,
            indicators=result.indicators,
            broker=broker_id,
            kind="binary" if is_binary else "exchange",
            expiry_at=expiry_at,
            payout=payout,
            owner_id=owner_id,
        )
        log.info(
            "Сигнал #%d %s %s @ %.4f (уверенность %d%%)",
            signal.id,
            signal.side,
            signal.symbol,
            signal.entry,
            signal.confidence,
        )
        await repo.log_event(
            "signal",
            f"{signal.side} {signal.symbol} @ {signal.entry:.4f}",
            {"signal_id": signal.id, "confidence": signal.confidence},
        )

        if self.on_signal is not None:
            try:
                await self.on_signal(signal)
            except Exception as exc:
                log.error("Не удалось отправить сигнал #%d: %s", signal.id, exc)

        return signal

    def _within_trade_hours(self, cfg=None) -> bool:
        """Попадает ли текущее время в заданное окно работы."""
        source = cfg or config
        window = str(source.get("trade_hours") or "").strip()
        if not window or "-" not in window:
            return True
        try:
            start, end = (p.strip() for p in window.split("-", 1))
            now_hm = datetime.now(settings.tz).strftime("%H:%M")
            if start == end:
                return True
            if start < end:
                return start <= now_hm < end
            # Окно через полночь, например 22:00-06:00
            return now_hm >= start or now_hm < end
        except Exception:
            return True

    # ----------------------------------------------------------------
    # Состояние для /status и Mini App
    # ----------------------------------------------------------------

    def _all_symbols(self) -> list[str]:
        """Объединённый список инструментов всех получателей."""
        out: set[str] = set()
        for owner_id in settings.owner_id_list:
            out.update(config.get("symbols", user_id=owner_id) or [])
        return sorted(out)

    def state(self, owner_id: int | None = None) -> dict:
        cfg = config.view(owner_id) if owner_id else config
        muted = calendar.mute_reason(cfg)
        symbols = (
            self._by_owner.get(owner_id, [])
            if owner_id
            else sorted({s for v in self._by_owner.values() for s in v})
        )
        return {
            "running": self.running,
            "symbols": symbols,
            "last_scan_at": self.last_scan_at,
            "scans_done": self.scans_done,
            "last_error": self.last_error,
            "muted_by_news": muted.to_dict() if muted else None,
            "within_hours": self._within_trade_hours(cfg),
            "trade_hours": str(cfg.get("trade_hours") or ""),
            "news_loaded": calendar.loaded,
        }
