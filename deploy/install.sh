#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then
    echo "Запусти установку от root."
    exit 1
fi

apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
install -d -m 755 /opt/cashback-bot
install -m 644 cashback.py /opt/cashback-bot/cashback.py
install -m 644 knowledge.json /opt/cashback-bot/knowledge.json
if [ -f november-2026.json ]; then
    install -m 644 november-2026.json /opt/cashback-bot/november-2026.json
fi
install -m 644 deploy/cashback-bot.service /etc/systemd/system/cashback-bot.service
if [ ! -e /etc/cashback-bot.env ]; then
    install -m 600 /dev/null /etc/cashback-bot.env
    printf '%s\n' 'TELEGRAM_BOT_TOKEN=' 'TELEGRAM_OWNER_ID=0' > /etc/cashback-bot.env
fi
systemctl daemon-reload
echo "Заполни /etc/cashback-bot.env и запусти systemctl enable --now cashback-bot"
