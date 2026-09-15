#!/usr/bin/env bash
#
# Автоматическое обновление бота на сервере.
#
# Раз в пару минут смотрит, не появилось ли в ветке новых коммитов.
# Если появились — собирает образ, ПРОГОНЯЕТ ТЕСТЫ и только потом
# подменяет работающий контейнер. Если что-то пошло не так, возвращает
# предыдущую версию и пишет об этом в Telegram.
#
# Запускается systemd-таймером (см. trading-bot-deploy.timer), но
# работает и руками:
#
#   /opt/trading-bot/deploy/autodeploy.sh            # обычный проход
#   /opt/trading-bot/deploy/autodeploy.sh --force    # пересобрать, даже
#                                                    # если коммитов нет
#   /opt/trading-bot/deploy/autodeploy.sh --no-tests # пропустить тесты
#
# Почему обновление «затягивающее», а не по вебхуку от GitHub: сервер
# домашний, за NAT и чужим reverse-proxy. Входящий вебхук пришлось бы
# пробрасывать через ещё один маршрут, и он тихо ломался бы при каждой
# смене адреса. Опрос работает всегда, пока сервер видит интернет.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/trading-bot}"
BRANCH="${BRANCH:-main}"
IMAGE="${IMAGE:-trading-signals-bot}"
CONTAINER="${CONTAINER:-trading-signals-bot}"
SERVICE="${SERVICE:-bot}"
LOG_FILE="${LOG_FILE:-/var/log/trading-bot-deploy.log}"
LOCK_FILE="${LOCK_FILE:-/var/lock/trading-bot-deploy.lock}"
BACKUPS_KEEP="${BACKUPS_KEEP:-10}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

FORCE=0
RUN_TESTS=1
for arg in "$@"; do
  case "$arg" in
    --force)    FORCE=1 ;;
    --no-tests) RUN_TESTS=0 ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

log() {
  printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

die() {
  log "ОШИБКА: $*"
  exit 1
}

# Значение из .env: без кавычек и без возврата каретки, если файл
# редактировали из Windows
read_env() {
  sed -n "s/^$1=//p" "$APP_DIR/.env" 2>/dev/null | head -1 \
    | tr -d '\r' | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# Сообщение в Telegram отправляем ИЗ КОНТЕЙНЕРА: у самого сервера доступа
# к api.telegram.org нет, через VPN ходит только докеровская подсеть.
notify() {
  local text="$1"
  [ -f "$APP_DIR/.env" ] || return 0

  local token owners net
  token="$(read_env BOT_TOKEN)"
  owners="$(read_env OWNER_IDS)"
  [ -n "$token" ] && [ -n "$owners" ] || return 0

  net="$(docker inspect "$CONTAINER" \
        --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null || true)"
  [ -n "$net" ] || net="$(basename "$APP_DIR")_default"

  docker run --rm --network="$net" \
    -e TG_TOKEN="$token" -e TG_OWNERS="$owners" -e TG_TEXT="$text" \
    "$IMAGE:latest" python -c '
import json, os, urllib.error, urllib.request

token = os.environ["TG_TOKEN"]
text = os.environ["TG_TEXT"]
for chat_id in os.environ["TG_OWNERS"].replace(" ", "").split(","):
    if not chat_id:
        continue
    body = json.dumps({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=15)
    except Exception as exc:
        print("не доставлено:", chat_id, exc)
' >>"$LOG_FILE" 2>&1 || log "предупреждение: уведомление в Telegram не ушло"
}

# --------------------------------------------------------------------------
# Один проход за раз
# --------------------------------------------------------------------------

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  # Предыдущий запуск ещё собирает образ — это нормально, просто ждём
  # следующего срабатывания таймера
  exit 0
fi

cd "$APP_DIR" || die "нет каталога $APP_DIR"
[ -d .git ] || die "$APP_DIR — не git-репозиторий. Разверните через deploy/install-autodeploy.sh"

# --------------------------------------------------------------------------
# Есть ли что обновлять
# --------------------------------------------------------------------------

git fetch --quiet origin "$BRANCH" || die "не удалось получить изменения из origin"

PREV_SHA="$(git rev-parse HEAD)"
NEXT_SHA="$(git rev-parse "origin/$BRANCH")"

if [ "$PREV_SHA" = "$NEXT_SHA" ] && [ "$FORCE" -eq 0 ]; then
  # Контейнер мог упасть между проходами — тогда поднимаем его обратно
  if ! docker ps --filter "name=^${CONTAINER}$" --filter "status=running" \
       --format '{{.Names}}' | grep -q .; then
    log "контейнер не запущен — поднимаю"
    docker compose up -d --no-build "$SERVICE" >>"$LOG_FILE" 2>&1 || true
    notify "⚠️ <b>Бот был остановлен, запустил обратно</b>"
  fi
  exit 0
fi

SUBJECT="$(git log --format=%s -1 "origin/$BRANCH")"
AUTHOR="$(git log --format=%an -1 "origin/$BRANCH")"
SHORT="$(git rev-parse --short "origin/$BRANCH")"

log "=========================================================="
log "новая версия: $SHORT — $SUBJECT (автор: $AUTHOR)"

# --------------------------------------------------------------------------
# Забираем код
# --------------------------------------------------------------------------

# .env, data/ и logs/ в .gitignore, поэтому reset их не трогает
git reset --hard --quiet "origin/$BRANCH" || die "не удалось переключиться на новую версию"
chmod +x deploy/*.sh 2>/dev/null || true

# Копия базы до всего остального: откатить код легко, потерянные
# настройки и журнал сигналов — нет
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -d data ]; then
  cp -a data "data.backup-$STAMP"
  log "база скопирована в data.backup-$STAMP"
  # Оставляем последние BACKUPS_KEEP копий, остальные удаляем
  ls -1dt data.backup-* 2>/dev/null | tail -n "+$((BACKUPS_KEEP + 1))" \
    | xargs -r rm -rf
fi

# --------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------

log "собираю образ"
# --network=host обязателен: у сборочной сети docker нет доступа к DNS
# из-за политики INPUT DROP на этом сервере
if ! docker build --network=host -t "$IMAGE:candidate" . >>"$LOG_FILE" 2>&1; then
  git reset --hard --quiet "$PREV_SHA"
  log "сборка не удалась, вернул код на $PREV_SHA"
  notify "🛑 <b>Обновление не установлено</b>
Не собрался образ.

<b>$SHORT</b> — $SUBJECT
Автор: $AUTHOR

Бот продолжает работать на прежней версии.
Подробности: <code>tail -50 $LOG_FILE</code>"
  exit 1
fi

# --------------------------------------------------------------------------
# Тесты в новом образе
# --------------------------------------------------------------------------

if [ "$RUN_TESTS" -eq 1 ]; then
  log "прогоняю тесты"
  NET="$(docker inspect "$CONTAINER" \
        --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null || true)"
  [ -n "$NET" ] || NET="$(basename "$APP_DIR")_default"

  # Тесты ходят на биржу, поэтому сеть нужна та же, что у бота:
  # наружу этот сервер выходит только через неё
  if ! timeout 600 docker run --rm --network="$NET" \
       "$IMAGE:candidate" python -m tests.test_smoke >>"$LOG_FILE" 2>&1; then
    git reset --hard --quiet "$PREV_SHA"
    log "тесты не прошли, вернул код на $PREV_SHA"
    notify "🛑 <b>Обновление не установлено</b>
Не прошли тесты.

<b>$SHORT</b> — $SUBJECT
Автор: $AUTHOR

Бот продолжает работать на прежней версии — на нём это никак
не сказалось. Посмотреть, что именно упало:
<code>grep -n FAIL $LOG_FILE | tail -20</code>"
    exit 1
  fi
  log "тесты пройдены"
fi

# --------------------------------------------------------------------------
# Подмена работающего контейнера
# --------------------------------------------------------------------------

# Предыдущий образ держим под рукой — на случай отката
docker image inspect "$IMAGE:latest" >/dev/null 2>&1 \
  && docker tag "$IMAGE:latest" "$IMAGE:previous"

docker tag "$IMAGE:candidate" "$IMAGE:latest"
log "перезапускаю бота"
docker compose up -d --no-build "$SERVICE" >>"$LOG_FILE" 2>&1 \
  || log "предупреждение: compose вернул ошибку, проверяю состояние"

# --------------------------------------------------------------------------
# Проверка здоровья и откат
# --------------------------------------------------------------------------

healthy=0
deadline=$((SECONDS + HEALTH_TIMEOUT))
while [ $SECONDS -lt $deadline ]; do
  state="$(docker inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
  health="$(docker inspect "$CONTAINER" \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            2>/dev/null || echo none)"
  if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "none" ]; }; then
    healthy=1
    break
  fi
  [ "$state" = "exited" ] && break
  sleep 5
done

if [ "$healthy" -ne 1 ]; then
  log "бот не поднялся, откатываюсь на $PREV_SHA"
  git reset --hard --quiet "$PREV_SHA"
  if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
    docker tag "$IMAGE:previous" "$IMAGE:latest"
  fi
  docker compose up -d --no-build "$SERVICE" >>"$LOG_FILE" 2>&1 || true
  notify "🛑 <b>Откатился на прежнюю версию</b>
Новая собралась и прошла тесты, но бот с ней не запустился.

<b>$SHORT</b> — $SUBJECT
Автор: $AUTHOR

Сейчас работает предыдущая версия.
Логи бота: <code>docker logs $CONTAINER --tail 50</code>"
  exit 1
fi

log "готово: работает $SHORT"

CHANGED="$(git diff --stat "$PREV_SHA" "$NEXT_SHA" -- . | tail -1 | sed 's/^ *//')"
notify "✅ <b>Бот обновлён</b>

<b>$SUBJECT</b>
Версия $SHORT, автор: $AUTHOR
${CHANGED:+Изменений: $CHANGED}

Обновление прошло само: код собран, тесты пройдены, бот перезапущен.
Ваши настройки, журнал сигналов и заказанные уведомления на месте."
