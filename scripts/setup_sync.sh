#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_EXT="/home/swair/.local/share/mise/installs/node/25.9.0/lib/node_modules/@swairshah/pi-talk/index.ts"
BROKER_UNIT="/home/swair/.config/systemd/user/pitalk-lite-broker.service"
TRAY_UNIT="/home/swair/.config/systemd/user/pitalk-lite-tray.service"

echo "[setup] Root: ${ROOT_DIR}"

# 1) Sync extension override (if present)
if [[ -f "${ROOT_DIR}/extension-overrides/pi-talk-index.ts" ]]; then
  echo "[setup] Syncing extension override -> ${TARGET_EXT}"
  install -m 0644 "${ROOT_DIR}/extension-overrides/pi-talk-index.ts" "${TARGET_EXT}"
else
  echo "[setup] No extension override found, skipping"
fi

# 2) Ensure user services point at this central repo path
cat >"${BROKER_UNIT}" <<EOF
[Unit]
Description=PiTalk Lite Linux TTS Broker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env python ${ROOT_DIR}/linux_tts_broker.py
Restart=on-failure
RestartSec=2
WorkingDirectory=${ROOT_DIR}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

cat >"${TRAY_UNIT}" <<EOF
[Unit]
Description=PiTalk Lite Tray App
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/env python ${ROOT_DIR}/pitalk_tray.py
Restart=on-failure
RestartSec=2
WorkingDirectory=${ROOT_DIR}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=graphical-session.target
EOF

# 3) Reload and restart services
systemctl --user daemon-reload
systemctl --user enable pitalk-lite-broker.service pitalk-lite-tray.service >/dev/null
systemctl --user restart pitalk-lite-broker.service pitalk-lite-tray.service

echo "[setup] Done."
echo "[setup] Status:"
systemctl --user --no-pager --lines=2 status pitalk-lite-broker.service pitalk-lite-tray.service || true
