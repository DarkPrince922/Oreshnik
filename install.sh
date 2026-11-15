#!/usr/bin/env bash
# Oreshnik: установка одной командой.
#
#   ./install.sh                 — панель на этом компьютере (http://localhost:5173)
#   ./install.sh 203.0.113.5     — панель на сервере: адрес, по которому вы будете её открывать (IP или домен)
#
# Что делает: проверяет Docker, создаёт .env со случайными секретами и паролем администратора, собирает и запускает
# контейнеры, ждёт, пока панель ответит, и печатает адрес, логин и пароль. Повторный запуск ничего не перезаписывает:
# существующий .env остаётся как есть, контейнеры просто пересобираются и поднимаются.
set -euo pipefail

cd "$(dirname "$0")"
HOST="${1:-localhost}"

say()  { printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

case "$HOST" in
  *[!A-Za-z0-9.:-]*|"") fail "Адрес должен быть IP или доменом без http:// и без пробелов, например: ./install.sh 203.0.113.5" ;;
esac

# ── 1. Docker ────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  say "Docker не найден — устанавливаю"
  if command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed --noconfirm docker docker-compose
  elif command -v apt-get >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || fail "Нужен curl: установите его (например, sudo apt-get install -y curl) и запустите снова."
    curl -fsSL https://get.docker.com | sudo sh
  else
    fail "Не знаю, как поставить Docker в этой системе. Установите Docker и Docker Compose сами: https://docs.docker.com/engine/install/"
  fi
  sudo systemctl enable --now docker >/dev/null 2>&1 || true
fi

# Without permission to talk to the daemon every docker command goes through sudo.
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  sudo systemctl start docker >/dev/null 2>&1 || true
  if sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  else
    fail "Docker установлен, но не запущен. Запустите его (sudo systemctl start docker) и повторите."
  fi
fi
$DOCKER compose version >/dev/null 2>&1 || fail "Нет Docker Compose v2 (команда «docker compose»). Установите пакет docker-compose-plugin (в Arch — docker-compose)."

# ── 2. .env со случайными секретами ──────────────────────────────────────
rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# A Fernet key is 32 random bytes in URL-safe base64.
rand_fernet() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }
set_env() {  # set_env KEY VALUE — replace the line KEY=... in .env (or add it)
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" 'BEGIN{done=0} index($0, k"=")==1 {print k"="v; done=1; next} {print} END{if(!done) print k"="v}' .env > "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}

ADMIN_PASSWORD_NEW=""
if [ -f .env ]; then
  say "Файл .env уже есть — оставляю его как есть"
else
  say "Создаю .env со случайными секретами"
  [ -f .env.example ] || fail "Не найден .env.example — запускайте скрипт из папки Oreshnik."
  cp .env.example .env
  chmod 600 .env
  ADMIN_PASSWORD_NEW="$(rand_hex 9)"
  set_env SECRET_ENCRYPTION_KEY "$(rand_fernet)"
  set_env JWT_SECRET "$(rand_hex 32)"
  set_env AGENT_TOKEN "$(rand_hex 24)"
  # A database that already exists keeps the password it was created with: only a fresh one gets a random password.
  if ! $DOCKER volume ls -q 2>/dev/null | grep -q '_pgdata$'; then
    set_env POSTGRES_PASSWORD "$(rand_hex 16)"
  fi
  set_env ADMIN_PASSWORD "$ADMIN_PASSWORD_NEW"
  if [ "$HOST" != "localhost" ]; then
    set_env VITE_API_BASE "http://$HOST:8000"
    set_env CORS_ORIGINS "[\"http://$HOST:5173\",\"http://localhost:5173\"]"
  fi
fi

# ── 3. Сборка и запуск ───────────────────────────────────────────────────
say "Собираю и запускаю контейнеры (первый раз это занимает 5–15 минут)"
$DOCKER compose up -d --build

say "Жду, пока панель ответит"
ready=0
for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null http://localhost:8000/api/health 2>/dev/null; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || fail "Панель не ответила за 3 минуты. Посмотрите, что случилось: $DOCKER compose logs backend --tail 50"

ADMIN_EMAIL="$(grep -m1 '^ADMIN_EMAIL=' .env | cut -d= -f2-)"
printf '\n\033[1;32m✓ Oreshnik запущен\033[0m\n\n'
printf '  Адрес:   http://%s:5173\n' "$HOST"
printf '  Логин:   %s\n' "${ADMIN_EMAIL:-admin@oreshnik.dev}"
if [ -n "$ADMIN_PASSWORD_NEW" ]; then
  printf '  Пароль:  %s\n\n' "$ADMIN_PASSWORD_NEW"
  printf '  Пароль показан один раз — сохраните его. Сменить можно в панели: «Аккаунт».\n'
else
  printf '  Пароль:  тот, что был задан при первой установке (строка ADMIN_PASSWORD в .env)\n'
fi
printf '\n  Остановить:  %s compose stop\n  Обновить:    git pull && ./install.sh\n\n' "$DOCKER"
