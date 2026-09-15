#!/usr/bin/env bash
#
# Однократная установка автообновления. Запускать на сервере от root:
#
#   bash deploy/install-autodeploy.sh
#
# После этого любой push в ветку main приезжает на сервер сам: таймер
# systemd раз в две минуты смотрит git, и если появились новые коммиты —
# собирает образ, гоняет тесты и перезапускает бота. Ничего настраивать
# тому, кто пушит, не нужно.
#
# Скрипт можно запускать повторно — он ничего не ломает.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/trading-bot}"
REPO="${REPO:-https://github.com/riskerdruid/traiding-bot-tg.git}"
BRANCH="${BRANCH:-main}"
LOG_FILE=/var/log/trading-bot-deploy.log

say()  { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n  \033[31m✗ %s\033[0m\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "нужны права root"

say "1/5  Проверяю окружение"
command -v docker  >/dev/null || die "не установлен docker"
command -v git     >/dev/null || die "не установлен git"
command -v systemctl >/dev/null || die "нет systemd"
[ -d "$APP_DIR" ] || die "нет каталога $APP_DIR"
[ -f "$APP_DIR/.env" ] || die "нет $APP_DIR/.env — сначала разверните бота"
ok "docker, git и systemd на месте"

# --------------------------------------------------------------------------
say "2/5  Связываю каталог с репозиторием"
# --------------------------------------------------------------------------

cd "$APP_DIR"

# Каталог, скопированный с рабочей машины по scp, приезжает с чужим
# владельцем — git такому каталогу не доверяет и отказывается работать.
# Служба ходит сюда от root, поэтому владельцем должен быть root.
if [ "$(stat -c %u .)" != "0" ]; then
  chown -R root:root .
  ok "владелец каталога исправлен на root"
fi
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

if [ -d .git ]; then
  git remote set-url origin "$REPO"
  ok "репозиторий уже подключён"
else
  # Каталог разворачивали копированием файлов, git-истории в нём нет.
  # Инициализируем прямо на месте: так .env, data/ и logs/ остаются
  # нетронутыми — git их не знает и не трогает.
  git init -q
  git remote add origin "$REPO"
  ok "git-репозиторий создан прямо в каталоге"
fi

git fetch --quiet origin "$BRANCH" || die "не получается достучаться до GitHub"

# checkout -f перезаписывает файлы репозитория и НЕ удаляет посторонние:
# настройки и база остаются на месте
git checkout -q -f -B "$BRANCH" "origin/$BRANCH"
git branch --quiet --set-upstream-to="origin/$BRANCH" "$BRANCH" 2>/dev/null || true
chmod +x deploy/*.sh
ok "рабочая копия на $(git rev-parse --short HEAD) — $(git log --format=%s -1)"

# Каталог с базой мог остаться от root, а бот пишет в него из контейнера
[ -d data ] || mkdir -p data
[ -d logs ] || mkdir -p logs

# --------------------------------------------------------------------------
say "3/5  Ставлю службу и таймер"
# --------------------------------------------------------------------------

install -m 0644 deploy/trading-bot-deploy.service /etc/systemd/system/
install -m 0644 deploy/trading-bot-deploy.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trading-bot-deploy.timer >/dev/null
ok "таймер включён: проверка каждые 2 минуты"

# --------------------------------------------------------------------------
say "4/5  Настраиваю журнал"
# --------------------------------------------------------------------------

touch "$LOG_FILE"
cat >/etc/logrotate.d/trading-bot-deploy <<'ROTATE'
/var/log/trading-bot-deploy.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
ROTATE
ok "журнал: $LOG_FILE, чистится раз в неделю"

# --------------------------------------------------------------------------
say "5/5  Пробное обновление"
# --------------------------------------------------------------------------

echo "  Собираю образ и прогоняю тесты — это займёт пару минут."
if "$APP_DIR/deploy/autodeploy.sh" --force; then
  ok "полный цикл обновления отработал"
else
  warn "пробное обновление завершилось с ошибкой — смотрите $LOG_FILE"
fi

cat <<INFO

  Готово. Дальше всё само:

    • любой push в ветку $BRANCH приезжает на сервер за 2 минуты;
    • перед подменой бота прогоняются тесты — сломанный код
      до сервера не доедет, бот останется на прежней версии;
    • если новая версия не запустится, сервер сам вернёт старую;
    • о каждом обновлении и о каждой неудаче приходит сообщение
      в Telegram.

  Полезные команды:

    systemctl list-timers trading-bot-deploy.timer   когда следующая проверка
    systemctl start trading-bot-deploy.service       проверить прямо сейчас
    tail -f $LOG_FILE      что происходит
    $APP_DIR/deploy/autodeploy.sh --force            пересобрать принудительно
    systemctl disable --now trading-bot-deploy.timer выключить автообновление

INFO
