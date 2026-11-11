#!/usr/bin/env bash
# Установка сервера «Рядом» на чистый VPS (Ubuntu 22.04/24.04, Debian 12).
# Запуск:  sudo bash install.sh remote.example.ru
#      или sudo bash install.sh 203.0.113.10      (если домена нет)
set -euo pipefail
cd "$(dirname "$0")"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
hdr() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { red "Запустите от root: sudo bash install.sh <домен или IP>"; exit 1; }

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  read -rp "Домен сервера (например remote.firma.ru) или IP: " DOMAIN
fi
DOMAIN="$(echo "$DOMAIN" | tr -d '[:space:]' | sed -e 's#^https\?://##' -e 's#/.*$##')"
[ -n "$DOMAIN" ] || { red "Домен или IP не указан"; exit 1; }

if [[ "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  MODE=ip
  API_URL="http://$DOMAIN:21114"
  PANEL_BIND=0.0.0.0
  TRUST_PROXY=0
  PROFILE=()
else
  MODE=https
  API_URL="https://$DOMAIN"
  PANEL_BIND=127.0.0.1
  TRUST_PROXY=1
  PROFILE=(--profile https)
  MYIP="$(curl -fsS4 https://ifconfig.me 2>/dev/null || true)"
  DNSIP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)"
  if [ -n "$MYIP" ] && [ "$MYIP" != "$DNSIP" ]; then
    red "Внимание: $DOMAIN указывает на '${DNSIP:-ничего}', а у этого сервера IP $MYIP."
    red "Сертификат HTTPS не выпустится, пока A-запись домена не будет указывать на $MYIP."
    read -rp "Продолжить всё равно? [y/N] " a; [[ "$a" =~ ^[YyДд] ]] || exit 1
  fi
fi

hdr "Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { red "Нужен Docker Compose v2 (пакет docker-compose-plugin)"; exit 1; }
grn "Docker готов"

hdr "Настройки"
if [ -f .env ]; then
  grn ".env уже есть — оставляю как есть (удалите его, чтобы перенастроить)"
  ADMIN_PASSWORD="$(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2-)"
  MODE_SAVED="$(grep '^PANEL_BIND=' .env | cut -d= -f2-)"
  [ "$MODE_SAVED" = "0.0.0.0" ] && PROFILE=() || PROFILE=(--profile https)
  DOMAIN="$(grep '^DOMAIN=' .env | cut -d= -f2-)"
  API_URL="$(grep '^API_URL=' .env | cut -d= -f2-)"
else
  ADMIN_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 14)"
  cat > .env <<EOF
DOMAIN=$DOMAIN
API_URL=$API_URL
APP_NAME=Рядом
URI_SCHEME=ryadom
ADMIN_USER=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
PANEL_BIND=$PANEL_BIND
TRUST_PROXY=$TRUST_PROXY
AUDIT_KEEP_DAYS=365
EOF
  chmod 600 .env
  grn ".env создан"
fi
mkdir -p data downloads panel-data caddy-data caddy-config
if [ -f id_ed25519 ] && [ ! -f data/id_ed25519 ]; then
  mv id_ed25519 data/id_ed25519
  chmod 600 data/id_ed25519
  grn "Использую выданный ключ сервера (server/id_ed25519)"
fi

hdr "Брандмауэр"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 21115:21117/tcp >/dev/null
  ufw allow 21116/udp >/dev/null
  if [ "$MODE" = https ]; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; else ufw allow 21114/tcp >/dev/null; fi
  grn "Порты открыты в ufw"
else
  echo "ufw не активен. Если у провайдера VPS есть свой файрвол, откройте:"
  echo "  TCP 21115-21117, UDP 21116, и $([ "$MODE" = https ] && echo 'TCP 80, 443' || echo 'TCP 21114')"
fi

hdr "Запуск"
docker compose "${PROFILE[@]}" up -d --build

printf "Жду ключ сервера"
for _ in $(seq 1 60); do
  [ -s data/id_ed25519.pub ] && break
  printf "."; sleep 1
done
echo
KEY="$(cat data/id_ed25519.pub 2>/dev/null || true)"
[ -n "$KEY" ] || { red "Ключ не появился — смотрите: docker compose logs hbbs"; exit 1; }

cat > client-settings.txt <<EOF
Параметры клиента «Рядом» (client/server.json в репозитории):
  Host (ID-сервер):  $DOMAIN
  Key:               $KEY
  API:               $API_URL
EOF

WANT_KEY="$(grep -o '"key": *"[^"]*"' ../client/server.json 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' || true)"
if [ -n "$WANT_KEY" ] && [ "$WANT_KEY" != "$KEY" ]; then
  red "Внимание: ключ сервера не совпадает с ключом в client/server.json."
  red "Собранные клиенты не подключатся. Положите выданный файл id_ed25519 в server/ и запустите установку заново,"
  red "или впишите новый ключ в client/server.json и пересоберите клиент."
fi

hdr "Готово"
cat <<EOF
Страница для сотрудников:  ${API_URL%/}/
Панель администратора:     ${API_URL%/}/admin
  логин:  admin
  пароль: $ADMIN_PASSWORD      (смените в разделе «Мой аккаунт»)

Для клиента:
  ID-сервер:  $DOMAIN
  Ключ:       $KEY
  API:        $API_URL
(сохранено в $(pwd)/client-settings.txt)

Установочные файлы клиентов кладите в $(pwd)/downloads — они сразу появятся на странице загрузки.
EOF
