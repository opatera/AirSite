#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${HOME}/.local/share/applications"
DESKTOP_DIR="${HOME}/Desktop"
LAUNCHER_FILE="${APP_DIR}/AirRetro.desktop"

if [[ ! -x "${REPO_DIR}/dist/AirRetro" ]]; then
  echo "Build AirRetro first: ${REPO_DIR}/dist/AirRetro is missing."
  exit 1
fi

mkdir -p "${APP_DIR}" "${DESKTOP_DIR}"

cat > "${LAUNCHER_FILE}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=AirRetro
Comment=Play your local music and GBA library
Exec=${REPO_DIR}/dist/AirRetro
Icon=${REPO_DIR}/assets/airsite-logo.png
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=true
EOF

chmod +x "${LAUNCHER_FILE}"
cp "${LAUNCHER_FILE}" "${DESKTOP_DIR}/AirRetro.desktop"
chmod +x "${DESKTOP_DIR}/AirRetro.desktop"

echo "AirRetro launcher installed in the Applications menu and on the Desktop."
