"""Запуск всего приложения: бот, сканер, трекер и веб-сервер в одном процессе.

Один процесс выбран сознательно: компонентов немного, они делят одну базу
и один клиент биржи, а разносить их по сервисам означало бы усложнить
развёртывание без выигрыша. Все части работают в общем event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time
from datetime import datetime

import uvicorn
from aiogram.exceptions import TelegramUnauthorizedError

from app.api import server as api_server
from app.bot import handlers as bot_handlers
from app.bot.instance import create_bot, create_dispatcher, setup_bot_profile
from app.bot.notifier import Notifier
from app.config import settings
from app.engine.alerts import AlertWatcher
from app.engine.scanner import Scanner
from app.engine.tracker import Tracker
from app.logging_conf import setup_logging
from app.market.feed import feed
from app.storage import repo
from app.storage.db import db
from app.storage.settings_store import config

log = logging.getLogger("runner")


class StartupError(Exception):
    """Понятная пользователю ошибка запуска — показывается без traceback."""


BANNER = r"""
  ______             _ _               ____        _
 /_  __/______ _____/ (_)__  ___ _    / __ )____  / /_
  / / / ___/ _ `/ __  / / _ \/ _ `/   / __  / __ \/ __/
 /_/ /_/   \_,_/\_,_/_/_//_/\_, /   /_____/\___/\__/
                           /___/    сигналы в Telegram
"""


class Application:
    def __init__(self) -> None:
        self.bot = None
        self.dispatcher = None
        self.notifier: Notifier | None = None
        self.scanner: Scanner | None = None
        self.tracker: Tracker | None = None
        self.alerts: AlertWatcher | None = None
        self.api: uvicorn.Server | None = None
        self.tasks: list[asyncio.Task] = []
        self.started_at = time.time()
        self._stopping = False

    # ----------------------------------------------------------------
    # Подготовка
    # ----------------------------------------------------------------

    def preflight(self) -> bool:
        """Проверяет конфигурацию до запуска и понятно объясняет проблемы."""
        problems = settings.validate_runtime()
        if not problems:
            return True

        print()
        print("  НЕ ХВАТАЕТ НАСТРОЕК ДЛЯ ЗАПУСКА")
        print("  " + "-" * 52)
        for problem in problems:
            print(f"  • {problem}")
        print()
        print("  Что сделать:")
        print("  1. Скопируйте .env.example в .env")
        print("  2. Заполните BOT_TOKEN и OWNER_IDS")
        print("  3. Запустите снова:  python run.py")
        print()
        return False

    async def setup(self) -> None:
        await db.init()
        await config.load()
        log.info(
            "Настройки загружены: %s, ТФ %s/%s, порог %s%%",
            ", ".join(config.get("symbols") or []),
            config.get("timeframe"),
            config.get("htf_timeframe"),
            config.get("min_confidence"),
        )

        self.bot = create_bot()
        self.dispatcher = create_dispatcher()
        self.notifier = Notifier(self.bot)

        self.scanner = Scanner(on_signal=self.notifier.send_signal)
        self.tracker = Tracker(on_outcome=self.notifier.send_outcome)
        # Уровни, заказанные человеком вручную, живут отдельно от сигналов:
        # следить приходится и за теми инструментами, которых нет в списке
        self.alerts = AlertWatcher(on_hit=self.notifier.send_price_alert)

        # Пробрасываем живые компоненты в обработчики бота и в API
        shared = {
            "scanner": self.scanner,
            "tracker": self.tracker,
            "alerts": self.alerts,
            "started_at": self.started_at,
            "notifier": self.notifier,
        }
        bot_handlers.runtime.update(shared)
        api_server.runtime.update(shared)

        try:
            me = await self.bot.get_me()
        except TelegramUnauthorizedError:
            raise StartupError(
                "Telegram не принял BOT_TOKEN.\n"
                "  Проверьте, что токен скопирован целиком и без пробелов.\n"
                "  Получить новый: @BotFather → /mybots → ваш бот → API Token"
            ) from None
        except Exception as exc:
            raise StartupError(
                f"Не удалось связаться с Telegram: {exc}\n"
                "  Проверьте интернет-соединение. Если Telegram блокируется "
                "провайдером, потребуется прокси или сервер за рубежом."
            ) from None

        log.info("Бот подключён: @%s (%s)", me.username, me.full_name)
        await setup_bot_profile(self.bot)

    # ----------------------------------------------------------------
    # Компоненты
    # ----------------------------------------------------------------

    async def _run_api(self) -> None:
        # Имя намеренно не `config` — так называются живые настройки,
        # импортированные выше, и затенять их здесь опасно
        uvicorn_config = uvicorn.Config(
            api_server.create_app(),
            host=settings.api_host,
            port=settings.api_port,
            log_level="warning",
            access_log=False,
        )
        self.api = uvicorn.Server(uvicorn_config)
        # Uvicorn по умолчанию перехватывает сигналы — здесь это мешает,
        # завершением управляет Application
        self.api.install_signal_handlers = lambda: None
        log.info(
            "Веб-сервер на http://%s:%d %s",
            settings.api_host,
            settings.api_port,
            f"(Mini App: {settings.webapp_url})" if settings.webapp_enabled else "",
        )
        await self.api.serve()

    async def _run_daily_report(self) -> None:
        """Отправляет сводку в заданное заказчиком время.

        Время перечитывается на каждой итерации, поэтому смена в приложении
        применяется в тот же день, без перезапуска.
        """
        sent_on: str | None = None
        while not self._stopping:
            raw = str(config.get("daily_report_at") or "").strip()
            if not raw or not config.get("daily_report"):
                await asyncio.sleep(45)
                continue
            try:
                hour, minute = (int(x) for x in raw.split(":"))
            except ValueError:
                log.warning("Время сводки '%s' не в формате ЧЧ:ММ — пропускаю", raw)
                await asyncio.sleep(300)
                continue

            now = datetime.now(settings.tz)
            today = now.strftime("%Y-%m-%d")
            if now.hour == hour and now.minute >= minute and sent_on != today:
                sent_on = today
                try:
                    await self.notifier.send_daily_report()
                    log.info("Сводка за день отправлена")
                except Exception as exc:
                    log.error("Не удалось отправить сводку: %s", exc)
            await asyncio.sleep(45)

    async def _run_housekeeping(self) -> None:
        """Раз в сутки подчищает журнал событий."""
        while not self._stopping:
            await asyncio.sleep(86400)
            with contextlib.suppress(Exception):
                await repo.prune_events()

    # ----------------------------------------------------------------
    # Жизненный цикл
    # ----------------------------------------------------------------

    async def start(self) -> None:
        await self.setup()

        self.tasks = [
            asyncio.create_task(self._run_api(), name="api"),
            self.scanner.start(),
            self.tracker.start(),
            self.alerts.start(),
            asyncio.create_task(self._run_daily_report(), name="daily"),
            asyncio.create_task(self._run_housekeeping(), name="housekeeping"),
        ]

        await repo.log_event("info", "Бот запущен")
        with contextlib.suppress(Exception):
            await self.notifier.send_startup()

        log.info("Всё запущено. Откройте бота в Telegram и отправьте /start")

        # Поллинг Telegram держит процесс живым
        await self.dispatcher.start_polling(
            self.bot,
            allowed_updates=self.dispatcher.resolve_used_update_types(),
            handle_signals=False,
        )

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("Останавливаюсь…")

        if self.scanner:
            await self.scanner.stop()
        if self.tracker:
            await self.tracker.stop()
        if self.alerts:
            await self.alerts.stop()

        if self.dispatcher:
            with contextlib.suppress(Exception):
                await self.dispatcher.stop_polling()

        if self.api:
            self.api.should_exit = True

        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

        with contextlib.suppress(Exception):
            await feed.close()
        if self.bot:
            with contextlib.suppress(Exception):
                await self.bot.session.close()
        with contextlib.suppress(Exception):
            await repo.log_event("info", "Бот остановлен")
        await db.close()

        log.info("Остановлен.")


async def main() -> int:
    setup_logging(settings.log_level)
    print(BANNER)

    app = Application()
    if not app.preflight():
        return 1

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def request_stop() -> None:
        stop_event.set()

    # На Windows add_signal_handler не поддерживается — там ловим KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, request_stop)

    runner = asyncio.create_task(app.start())
    waiter = asyncio.create_task(stop_event.wait())

    try:
        done, _ = await asyncio.wait(
            {runner, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is runner:
                task.result()  # проброс исключения, если упал запуск
    except KeyboardInterrupt:
        pass
    except StartupError as exc:
        print()
        print("  НЕ УДАЛОСЬ ЗАПУСТИТЬСЯ")
        print("  " + "-" * 52)
        for line in str(exc).splitlines():
            print(f"  {line}")
        print()
        await app.shutdown()
        return 1
    except Exception as exc:
        log.exception("Критическая ошибка: %s", exc)
        await app.shutdown()
        return 1
    finally:
        waiter.cancel()
        runner.cancel()
        await app.shutdown()

    return 0


def run() -> None:
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)
