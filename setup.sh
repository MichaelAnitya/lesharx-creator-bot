#!/usr/bin/env bash
# One-command server setup for the LeSharX Creator Rewards bot.
# Target: a fresh Ubuntu VM (GCP e2-micro / Oracle Always Free).
# Usage:  curl -fsSL https://raw.githubusercontent.com/MichaelAnitya/lesharx-creator-bot/main/setup.sh | sudo bash
# Then:   sudo nano /etc/lesharx-bot.env   (fill in the token + IDs)
#         sudo systemctl restart lesharx-bot
set -euo pipefail

APP_DIR=/opt/lesharx-bot
ENV_FILE=/etc/lesharx-bot.env
REPO=https://github.com/MichaelAnitya/lesharx-creator-bot.git

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip > /dev/null

echo "==> Fetching the bot"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone -q "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR/data"

echo "==> Python environment"
if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  echo "==> Creating config template at $ENV_FILE"
  cat > "$ENV_FILE" <<'EOF'
# LeSharX Creator Rewards bot configuration.
# Fill these in, then: sudo systemctl restart lesharx-bot
DISCORD_TOKEN=paste-token-here
GUILD_ID=0
SUBMIT_CHANNEL_ID=0
ANNOUNCE_CHANNEL_ID=0
MOD_ROLE_ID=0
SEASON_START=2026-08-18
SEASON_END=2026-09-01
DB_PATH=/opt/lesharx-bot/data/creator_rewards.db
EOF
  chmod 600 "$ENV_FILE"
fi

echo "==> Installing systemd service (auto-start on boot, auto-restart on crash)"
cat > /etc/systemd/system/lesharx-bot.service <<EOF
[Unit]
Description=LeSharX Creator Rewards Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lesharx-bot > /dev/null 2>&1

if grep -q "paste-token-here" "$ENV_FILE"; then
  echo ""
  echo "============================================================"
  echo " Setup complete. Now add your token and IDs:"
  echo "   sudo nano $ENV_FILE"
  echo " then start the bot:"
  echo "   sudo systemctl restart lesharx-bot"
  echo " check it's running:"
  echo "   systemctl status lesharx-bot"
  echo "   journalctl -u lesharx-bot -f"
  echo "============================================================"
else
  systemctl restart lesharx-bot
  echo "==> Bot (re)started. Logs: journalctl -u lesharx-bot -f"
fi
