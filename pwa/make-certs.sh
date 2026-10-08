#!/usr/bin/env bash
# Сертификаты для планшета. Один раз создаёт свой корневой сертификат Ксении (его ставят на планшет),
# затем — сертификат шлюза для адресов этого компьютера. Повторный запуск (сменился адрес в сети)
# пересоздаёт только сертификат шлюза: на планшете ничего переустанавливать не нужно.
# Всё лежит в data/pwa/ (вне git). Закрытые ключи — только для владельца.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="data/pwa"
mkdir -p "$DIR"
chmod 700 "$DIR"
umask 077

NAME="$(hostname -s)"
# адреса этого компьютера в домашней сети (IPv4), можно добавить свои: make-certs.sh 192.168.1.50 ksenia.lan
IPS="$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]}' || hostname -I 2>/dev/null || true)"
EXTRA=("$@")

SAN="DNS:${NAME}.local,DNS:${NAME},DNS:localhost,IP:127.0.0.1"
HOSTS="\"${NAME}.local\", \"${NAME}\""
for ip in $IPS; do SAN="${SAN},IP:${ip}"; HOSTS="${HOSTS}, \"${ip}\""; done
for x in "${EXTRA[@]}"; do
  if [[ "$x" =~ ^[0-9.]+$ ]]; then SAN="${SAN},IP:${x}"; else SAN="${SAN},DNS:${x}"; fi
  HOSTS="${HOSTS}, \"${x}\""
done

if [ ! -f "$DIR/ca.key" ]; then
  openssl req -x509 -new -newkey rsa:3072 -nodes -sha256 -days 3650 \
    -keyout "$DIR/ca.key" -out "$DIR/ca.crt" -subj "/CN=Ksenia Home CA/O=Ksenia" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
  echo "Создан корневой сертификат: $DIR/ca.crt (его ставят на планшет)"
fi

openssl req -new -newkey rsa:2048 -nodes -sha256 -keyout "$DIR/server.key" -out "$DIR/server.csr" \
  -subj "/CN=${NAME}.local/O=Ksenia" >/dev/null 2>&1
EXT="$(mktemp)"
trap 'rm -f "$EXT" "$DIR/server.csr"' EXIT
printf "subjectAltName=%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n" "$SAN" > "$EXT"
# 825 дней: дольше браузеры не принимают даже от своего корня
openssl x509 -req -in "$DIR/server.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" -CAcreateserial \
  -out "$DIR/server.crt" -days 825 -sha256 -extfile "$EXT" >/dev/null 2>&1
chmod 644 "$DIR/ca.crt" "$DIR/server.crt"
echo "[${HOSTS}]" > "$DIR/hosts.json"

echo "Сертификат шлюза: $DIR/server.crt"
echo "Адреса: ${SAN}"
echo
echo "Дальше на планшете:"
for ip in $IPS; do echo "  1) откройте http://${ip}:18141/ — там сертификат и инструкция"; done
echo "  2) после установки сертификата: https://<адрес>:18140/"
echo "Перезапустите шлюз: systemctl --user restart ksenia-pwa"
