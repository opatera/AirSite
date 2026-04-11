#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="/etc/ssl/airsite"
KEY_FILE="$CERT_DIR/airsite.key"
CERT_FILE="$CERT_DIR/airsite.crt"
TMP_CONFIG="$(mktemp)"

cleanup() {
  rm -f "$TMP_CONFIG"
}

trap cleanup EXIT

if [[ "${EUID}" -ne 0 ]]; then
  exec sudo "$REPO_DIR/deploy/generate-cert.sh" "$@"
fi

declare -a SAN_ENTRIES=(
  "DNS:AirServer"
  "DNS:localhost"
  "IP:127.0.0.1"
)

for item in "$@"; do
  if [[ "$item" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    SAN_ENTRIES+=("IP:$item")
  else
    SAN_ENTRIES+=("DNS:$item")
  fi
done

SAN_LIST="$(printf '%s,' "${SAN_ENTRIES[@]}")"
SAN_LIST="${SAN_LIST%,}"

mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"

cat > "$TMP_CONFIG" <<EOF
[req]
default_bits = 4096
prompt = no
default_md = sha256
x509_extensions = v3_req
distinguished_name = dn

[dn]
CN = AirServer

[v3_req]
subjectAltName = ${SAN_LIST}
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EOF

openssl req \
  -x509 \
  -nodes \
  -days 825 \
  -newkey rsa:4096 \
  -keyout "$KEY_FILE" \
  -out "$CERT_FILE" \
  -config "$TMP_CONFIG"

chmod 600 "$KEY_FILE"
chmod 644 "$CERT_FILE"

echo "Certificate written to $CERT_FILE"
echo "Private key written to $KEY_FILE"
echo "Subject Alt Names: $SAN_LIST"
