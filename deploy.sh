#!/usr/bin/env bash
#
# Установка бота на сервер одной командой.
#
#   ./deploy.sh
#
# Домен и сертификат нужны только для мини-приложения. Без него достаточно,
# чтобы сервер видел интернет: бот сам звонит в Telegram.

set -euo pipefail

say()  { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n  \033[31m✗ %s\033[0m\n\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")"

say "1/4  Проверяю окружение"

command -v docker >/dev/null 2>&1 || die "Docker не установлен. https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Нужен Docker Compose v2 (docker compose, не docker-compose)"
ok "Docker на месте"

[ -f .env ] || {
  cp .env.example .env
  die ".env создан из шаблона. Заполните BOT_TOKEN и OWNER_IDS, затем запустите снова."
}

set -a; . ./.env; set +a

[ -n "${BOT_TOKEN:-}" ] || die "В .env не заполнен BOT_TOKEN (получить у @BotFather)"
[ -n "${OWNER_IDS:-}" ] || die "В .env не заполнен OWNER_IDS (узнать у @userinfobot)"
ok "Настройки заполнены"

if [ -n "${WEBAPP_URL:-}" ]; then
  case "$WEBAPP_URL" in
    https://*) ok "Мини-приложение: $WEBAPP_URL" ;;
    *) die "WEBAPP_URL должен начинаться с https:// — Telegram открывает приложения только по HTTPS" ;;
  esac
else
  ok "Мини-приложение выключено (WEBAPP_URL пуст) — бот работает сообщениями"
fi

say "2/4  Собираю образ"

mkdir -p data logs
# --network=host помогает на серверах, где сборочная сеть docker
# не видит DNS. На обычных серверах тоже безвреден.
docker build --network=host -t trading-signals-bot:latest . >/dev/null
ok "Образ собран"

say "3/4  Прогоняю проверки"

if docker run --rm trading-signals-bot:latest python -m tests.test_smoke >/tmp/bot-tests.log 2>&1; then
  ok "Проверки пройдены"
else
  tail -30 /tmp/bot-tests.log
  die "Проверки не прошли — бот не запущен"
fi

say "4/4  Запускаю"

docker compose up -d --no-build
ok "Контейнер поднят"

for i in $(seq 1 30); do
  state="$(docker inspect trading-signals-bot --format '{{.State.Status}}' 2>/dev/null || echo missing)"
  [ "$state" = "running" ] && break
  [ "$i" = 30 ] && die "Бот не поднялся. Логи: docker compose logs bot"
  sleep 2
done
ok "Бот работает"

cat <<INFO

  ГОТОВО

  Логи:        docker compose logs -f bot
  Перезапуск:  docker compose restart bot
  Обновление:  git pull && ./deploy.sh

  Дальше: откройте бота в Telegram и отправьте /start.
  Всё остальное настраивается кнопками внутри бота.

  Мини-приложение слушает ${BIND_HOST:-127.0.0.1}:${BIND_PORT:-8080} —
  проксируйте туда HTTPS-домен. Образец: deploy/nginx-vhost.conf

INFO
