#!/usr/bin/env bash
# Oreshnik: установка и обновление одной командой.
#
#   ./install.sh                 — панель на этом компьютере (http://localhost:5173)
#   ./install.sh 203.0.113.5     — панель на сервере: адрес, по которому вы будете её открывать (IP или домен)
#
# Что делает: проверяет Docker, создаёт .env со случайными секретами и паролем администратора (или чинит тот, что уже есть:
# дописывает недостающее, заменяет заглушки CHANGE_ME), подбирает число одновременных сканов под память машины, собирает и
# запускает контейнеры, ждёт, пока панель ответит, и печатает адрес, логин и пароль. Запускать повторно безопасно: рабочие
# значения в .env и данные он не трогает.
set -euo pipefail

cd "$(dirname "$0")"
HOST="${1:-localhost}"

say()  { printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '\n\033[1;33m! %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

case "$HOST" in
  *[!A-Za-z0-9.:-]*|"") fail "Адрес должен быть IP или доменом без http:// и без пробелов, например: ./install.sh 203.0.113.5" ;;
esac

SUDO="sudo"
[ "$(id -u)" = 0 ] && SUDO=""

# ── 1. Docker ────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  say "Docker не найден — устанавливаю"
  if command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -S --needed --noconfirm docker docker-compose
  elif command -v apt-get >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || fail "Нужен curl: установите его (например, sudo apt-get install -y curl) и запустите снова."
    curl -fsSL https://get.docker.com | $SUDO sh
  else
    fail "Не знаю, как поставить Docker в этой системе. Установите Docker и Docker Compose сами: https://docs.docker.com/engine/install/"
  fi
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
fi

# Without permission to talk to the daemon every docker command goes through sudo.
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  $SUDO systemctl start docker >/dev/null 2>&1 || true
  if $SUDO docker info >/dev/null 2>&1; then
    DOCKER="$SUDO docker"
  else
    fail "Docker установлен, но не запущен. Запустите его (sudo systemctl start docker) и повторите."
  fi
fi
$DOCKER compose version >/dev/null 2>&1 || fail "Нет Docker Compose v2 (команда «docker compose»). Установите пакет docker-compose-plugin (в Arch — docker-compose)."

# ── 2. .env: создать или починить ────────────────────────────────────────
rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# A Fernet key is 32 random bytes in URL-safe base64 (44 characters, "=" at the end).
rand_fernet() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }
get_env() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }
set_env() {  # set_env KEY VALUE — replace the line KEY=... in .env (or add it)
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" 'BEGIN{done=0} index($0, k"=")==1 {print k"="v; done=1; next} {print} END{if(!done) print k"="v}' .env > "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}

# How many scans this machine can run at once: about 2.5 GB of memory each (Strix about 1.6 GB, its sandbox about 0.5 GB,
# measured on a real server), after 1.5 GB for the panel itself.
MEM_MB="$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
SAFE_SCANS=1
if [ "${MEM_MB:-0}" -gt 0 ]; then
  SAFE_SCANS=$(( (MEM_MB - 1500) / 2500 ))
  [ "$SAFE_SCANS" -lt 1 ] && SAFE_SCANS=1
  [ "$SAFE_SCANS" -gt 16 ] && SAFE_SCANS=16
fi

ADMIN_PASSWORD_NEW=""
if [ ! -f .env ]; then
  say "Создаю .env со случайными секретами"
  [ -f .env.example ] || fail "Не найден .env.example — запускайте скрипт из папки Oreshnik."
  cp .env.example .env
  chmod 600 .env
  ADMIN_PASSWORD_NEW="$(rand_hex 9)"
  set_env ADMIN_PASSWORD "$ADMIN_PASSWORD_NEW"
  # A database that already exists keeps the password it was created with: only a fresh one gets a random password.
  if ! $DOCKER volume ls -q 2>/dev/null | grep -q '_pgdata$'; then
    set_env POSTGRES_PASSWORD "$(rand_hex 16)"
  fi
  set_env SCAN_CONCURRENCY "$SAFE_SCANS"
  note "Сканов одновременно: $SAFE_SCANS (памяти ${MEM_MB} МБ, на скан нужно около 2,5 ГБ). Меняется в панели: «Настройки»."
else
  say "Файл .env уже есть — проверяю его"
fi

# These three are checked on every run: an .env from an older version may hold placeholders or lack a line entirely.
key="$(get_env SECRET_ENCRYPTION_KEY)"
case "$key" in
  ""|*CHANGE_ME*) set_env SECRET_ENCRYPTION_KEY "$(rand_fernet)"; note "SECRET_ENCRYPTION_KEY: создан новый ключ шифрования (прежний был заглушкой)" ;;
esac
jwt="$(get_env JWT_SECRET)"
case "$jwt" in
  ""|*CHANGE_ME*) set_env JWT_SECRET "$(rand_hex 32)"; note "JWT_SECRET: создан (прежний был заглушкой)" ;;
esac
if [ -z "$(get_env AGENT_TOKEN)" ]; then
  set_env AGENT_TOKEN "$(rand_hex 24)"
  note "AGENT_TOKEN: создан — раздел «Агент» включён"
fi
# The server setup: the panel and its API at one address, behind nginx (docker-compose.prod.yml). Plain `docker compose ...`
# commands pick both files up from this line.
if [ -z "$(get_env COMPOSE_FILE)" ]; then
  set_env COMPOSE_FILE "docker-compose.yml:docker-compose.prod.yml"
  note "COMPOSE_FILE: панель и её API теперь на одном адресе (порт 5173) — VITE_API_BASE и CORS_ORIGINS настраивать не нужно"
fi

current="$(get_env SCAN_CONCURRENCY)"
if [ -n "$current" ] && [ "$current" -gt "$SAFE_SCANS" ] 2>/dev/null; then
  warn "SCAN_CONCURRENCY=$current, а памяти (${MEM_MB} МБ) хватает примерно на $SAFE_SCANS. При большем числе сканов сервер уйдёт в своп,"
  note "запросы к модели начнут обрываться по таймауту. Уменьшите число в панели («Настройки» → «Сканов одновременно») или в .env."
fi

# ── 3. Настройка ядра для Redis ──────────────────────────────────────────
if [ "$(cat /proc/sys/vm/overcommit_memory 2>/dev/null || echo 1)" != "1" ]; then
  say "Включаю vm.overcommit_memory=1 (Redis просит это, иначе может не сохранить данные при нехватке памяти)"
  if $SUDO sysctl -w vm.overcommit_memory=1 >/dev/null 2>&1; then
    echo "vm.overcommit_memory = 1" | $SUDO tee /etc/sysctl.d/99-oreshnik.conf >/dev/null 2>&1 || true
  else
    note "не получилось — не страшно, Redis будет только предупреждать в логе"
  fi
fi

# ── 4. Сборка и запуск ───────────────────────────────────────────────────
say "Собираю и запускаю контейнеры (первый раз это занимает 5–15 минут)"
# Every service reads .env, so all of them are brought up together: a container left running would keep its old settings.
$DOCKER compose up -d --build --remove-orphans

say "Жду, пока панель ответит"
ready=0
for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null http://localhost:8000/api/health 2>/dev/null; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || fail "Панель не ответила за 3 минуты. Посмотрите, что случилось: $DOCKER compose logs backend --tail 50"

ADMIN_EMAIL="$(get_env ADMIN_EMAIL)"
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
