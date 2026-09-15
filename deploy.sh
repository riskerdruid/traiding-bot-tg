#!/usr/bin/env bash
#
# Установка бота на сервер одной командой.
#
#   ./deploy.sh
#
# Скрипт проверит окружение, соберёт образ, поднимет бота и Caddy
# с автоматическим сертификатом, а затем убедится, что домен реально
# отвечает по HTTPS.

set -euo pipefail

say()  { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n  \033[31m✗ %s\033[0m\n\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")"

say "1/5  Проверяю окружение"

command -v docker >/dev/null 2>&1 || die "Docker не установлен. https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Нужен Docker Compose v2 (docker compose, не docker-compose)"
ok "Docker на месте"

[ -f .env ] || {
  cp .env.example .env
  die ".env создан из шаблона. Заполните BOT_TOKEN, OWNER_IDS и DOMAIN, затем запустите снова."
}

set -a; . ./.env; set +a

[ -n "${BOT_TOKEN:-}" ]  || die "В .env не заполнен BOT_TOKEN (получить у @BotFather)"
[ -n "${OWNER_IDS:-}" ] || die "В .env не заполнен OWNER_IDS (узнать у @userinfobot)"
[ -n "${DOMAIN:-}" ]    || die "В .env не заполнен DOMAIN"

# Частая ошибка: домен есть, а WEBAPP_URL забыли — тогда кнопка приложения
# в боте просто не появится, и это выглядит как «Mini App не работает».
if [ -z "${WEBAPP_URL:-}" ]; then
  warn "WEBAPP_URL пуст — кнопка Mini App не появится."
  warn "Впишите в .env:  WEBAPP_URL=https://${DOMAIN}"
  read -r -p "  Вписать автоматически и продолжить? [Y/n] " answer </dev/tty || answer=n
  case "${answer:-Y}" in
    [Nn]*) die "Отменено. Заполните WEBAPP_URL и запустите снова." ;;
    *)
      if grep -q '^WEBAPP_URL=' .env; then
        sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=https://${DOMAIN}|" .env
      else
        printf '\nWEBAPP_URL=https://%s\n' "$DOMAIN" >> .env
      fi
      WEBAPP_URL="https://${DOMAIN}"
      ok "WEBAPP_URL=${WEBAPP_URL}"
      ;;
  esac
elif [ "${WEBAPP_URL}" != "https://${DOMAIN}" ]; then
  warn "WEBAPP_URL (${WEBAPP_URL}) не совпадает с DOMAIN (${DOMAIN})."
  warn "Обычно они должны быть одинаковыми, иначе приложение не откроется."
fi

ok "Настройки заполнены: домен ${DOMAIN}"

say "2/5  Проверяю домен"

server_ip=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo "")
domain_ip=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || echo "")

if [ -z "$domain_ip" ]; then
  warn "$DOMAIN пока не резолвится. A-запись пропишется в течение нескольких минут."
  warn "Caddy не сможет получить сертификат, пока домен не указывает на этот сервер."
elif [ -n "$server_ip" ] && [ "$domain_ip" != "$server_ip" ]; then
  warn "$DOMAIN указывает на $domain_ip, а этот сервер — $server_ip"
  warn "Проверьте A-запись, иначе сертификат не выпустится."
else
  ok "$DOMAIN -> $domain_ip"
fi

for port in 80 443; do
  if ss -ltn 2>/dev/null | grep -q ":$port "; then
    warn "Порт $port уже занят. Остановите тот сервис, иначе Caddy не стартует."
  fi
done

say "3/5  Собираю и запускаю"

mkdir -p data logs logs/caddy
docker compose up -d --build
ok "Контейнеры подняты"

say "4/5  Жду готовности"

for i in $(seq 1 30); do
  if docker compose exec -T bot python -c "
import urllib.request,sys
try: sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=3).status==200 else 1)
except Exception: sys.exit(1)
" 2>/dev/null; then
    ok "Бот отвечает"
    break
  fi
  [ "$i" = 30 ] && die "Бот не поднялся за 60 секунд. Логи: docker compose logs bot"
  sleep 2
done

say "5/5  Проверяю HTTPS"

https_ok=false
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://${DOMAIN}/api/health" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then https_ok=true; break; fi
  sleep 5
done

if $https_ok; then
  ok "https://${DOMAIN} работает, сертификат выпущен"
else
  warn "https://${DOMAIN} пока не отвечает."
  warn "Обычно это значит, что DNS ещё не разошёлся или порты 80/443 закрыты."
  warn "Посмотреть, что происходит: docker compose logs caddy"
fi

cat <<INFO

  ГОТОВО

  Mini App:     https://${DOMAIN}
  Логи бота:    docker compose logs -f bot
  Логи Caddy:   docker compose logs -f caddy
  Перезапуск:   docker compose restart bot
  Обновление:   git pull && docker compose up -d --build

  Дальше: откройте бота в Telegram и отправьте /start.
  Все настройки — внутри приложения, в .env заходить больше не нужно.

INFO
